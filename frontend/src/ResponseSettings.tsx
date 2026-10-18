import { useEffect, useRef, useState } from "react";
import { api, type Observation, type Task } from "./types";

export type ResponseConfig = {
  mode: "website" | "custom";
  status: number;
  format: "json" | "text" | "html";
  body: string;
  delay_ms: number;
  headers: { name: string; value: string }[];
};
export type ResponseDraft = {
  config: ResponseConfig;
  baseVersion: number;
};
type Saved = {
  version: number;
  config: ResponseConfig;
  history: number[];
  state: { applied_version: number; error?: string } | null;
};

export default function ResponseSettings({
  labId,
  nodeId,
  draft,
  onDraftChange,
  observation,
  stale,
  disabled,
  tasks,
  apply,
}: {
  labId: string;
  nodeId: string;
  draft?: ResponseDraft;
  onDraftChange: (draft?: ResponseDraft) => void;
  observation?: Observation;
  stale: boolean;
  disabled: boolean;
  tasks: Task[];
  apply: (version: number) => Promise<void>;
}) {
  const [saved, setSaved] = useState<Saved | null>(null);
  const [config, setConfig] = useState<ResponseConfig | null>(null);
  const [draftBaseVersion, setDraftBaseVersion] = useState<number | null>(
    draft?.baseVersion ?? null,
  );
  const [error, setError] = useState("");
  const [working, setWorking] = useState(false);
  const initialDraft = useRef(draft);
  const path = `/labs/${labId}/nodes/${nodeId}/response`;
  const load = async () => {
    const value = await api<Saved>(path);
    setSaved(value);
    setConfig(value.config);
    setDraftBaseVersion(value.version);
    onDraftChange(undefined);
    setError("");
  };
  const updateConfig = (next: ResponseConfig) => {
    setConfig(next);
    if (!saved) return;
    if (JSON.stringify(next) === JSON.stringify(saved.config)) {
      setDraftBaseVersion(saved.version);
      onDraftChange(undefined);
      return;
    }
    const baseVersion = draftBaseVersion ?? saved.version;
    setDraftBaseVersion(baseVersion);
    onDraftChange({ config: next, baseVersion });
  };
  useEffect(() => {
    let active = true;
    api<Saved>(path)
      .then((value) => {
        if (active) {
          setSaved(value);
          const cached = initialDraft.current;
          setConfig(cached?.config ?? value.config);
          setDraftBaseVersion(cached?.baseVersion ?? value.version);
        }
      })
      .catch((e) => {
        if (active) setError(String(e));
      });
    return () => {
      active = false;
    };
  }, [path]);
  const dirty =
    config && JSON.stringify(config) !== JSON.stringify(saved?.config);
  const capable = observation?.custom_response;
  const runtime =
    !stale &&
    observation?.status === "running" &&
    observation.response?.available
      ? observation.response.version
      : null;
  const latest = tasks.find(
    (t) => t.kind === "apply_response" && t.payload.action.node_id === nodeId,
  );
  async function save(andApply: boolean) {
    if (!config || !saved) return;
    setWorking(true);
    setError("");
    try {
      let value = saved;
      if (dirty || !saved.version) {
        value = await api<Saved>(path, "PUT", {
          base_version: draftBaseVersion ?? saved.version,
          config,
        });
        setSaved(value);
        setConfig(value.config);
        setDraftBaseVersion(value.version);
        onDraftChange(undefined);
      }
      if (andApply) await apply(value.version);
    } catch (e) {
      setError(String(e));
    } finally {
      setWorking(false);
    }
  }
  return (
    <section className="response-settings">
      <div className="section-label spaced">响应设置</div>
      {error && (
        <p role="alert" className="error">
          {error}{" "}
          <button onClick={() => void load().catch((e) => setError(String(e)))}>
            重新读取
          </button>
        </p>
      )}
      {!capable && (
        <p className="hint">
          主动“应用拓扑”升级此应用后可使用；已保存配置不会自动生效。
        </p>
      )}
      {config && (
        <fieldset disabled={working || disabled}>
          <label className="full-field">
            模式
            <select
              value={config.mode}
              disabled={working}
              onChange={(e) =>
                updateConfig({ ...config, mode: e.target.value as ResponseConfig["mode"] })
              }
            >
              <option value="website">默认网站</option>
              <option value="custom">自定义响应</option>
            </select>
          </label>
          {config.mode === "custom" && (
            <>
              <div className="field-row">
                <label>
                  状态码
                  <input
                    type="number"
                    min={200}
                    max={599}
                    value={config.status}
                    onChange={(e) =>
                      updateConfig({ ...config, status: Number(e.target.value) })
                    }
                  />
                </label>
                <label>
                  延迟 / ms
                  <input
                    type="number"
                    min={0}
                    max={10000}
                    value={config.delay_ms}
                    onChange={(e) =>
                      updateConfig({ ...config, delay_ms: Number(e.target.value) })
                    }
                  />
                </label>
              </div>
              <label className="full-field">
                正文类型
                <select
                  value={config.format}
                  onChange={(e) =>
                    updateConfig({
                      ...config,
                      format: e.target.value as ResponseConfig["format"],
                    })
                  }
                >
                  <option value="json">JSON</option>
                  <option value="text">文本</option>
                  <option value="html">HTML</option>
                </select>
              </label>
              <label className="full-field">
                正文
                <textarea
                  aria-label="响应正文"
                  rows={8}
                  value={config.body}
                  onChange={(e) =>
                    updateConfig({ ...config, body: e.target.value })
                  }
                />
              </label>
              <div className="section-label spaced">
                响应头{" "}
                <button
                  disabled={config.headers.length >= 32}
                  onClick={() =>
                    updateConfig({
                      ...config,
                      headers: [...config.headers, { name: "", value: "" }],
                    })
                  }
                >
                  添加
                </button>
              </div>
              {config.headers.map((h, i) => (
                <div className="response-header" key={i}>
                  <input
                    aria-label={`响应头 ${i + 1} 名称`}
                    placeholder="名称"
                    value={h.name}
                    onChange={(e) =>
                      updateConfig({
                        ...config,
                        headers: config.headers.map((v, j) =>
                          j === i ? { ...v, name: e.target.value } : v,
                        ),
                      })
                    }
                  />
                  <input
                    aria-label={`响应头 ${i + 1} 值`}
                    placeholder="值"
                    value={h.value}
                    onChange={(e) =>
                      updateConfig({
                        ...config,
                        headers: config.headers.map((v, j) =>
                          j === i ? { ...v, value: e.target.value } : v,
                        ),
                      })
                    }
                  />
                  <button
                    aria-label={`删除响应头 ${i + 1}`}
                    onClick={() =>
                      updateConfig({
                        ...config,
                        headers: config.headers.filter((_, j) => j !== i),
                      })
                    }
                  >
                    ×
                  </button>
                </div>
              ))}
            </>
          )}
          <p className="hint">
            {dirty ? "未保存修改 · " : ""}已保存 v{saved?.version ?? 0} · 运行{" "}
            {runtime == null ? "未知 / 未运行" : `v${runtime}`}
          </p>
          <div className="response-actions">
            <button
              disabled={working || disabled || (!dirty && !!saved?.version)}
              onClick={() => void save(false)}
            >
              保存响应
            </button>
            <button
              className="primary"
              disabled={
                working ||
                disabled ||
                stale ||
                !capable ||
                observation?.status !== "running"
              }
              onClick={() => void save(true)}
            >
              应用响应配置
            </button>
          </div>
          {latest && (
            <p className="hint">
              应用 v{latest.payload.action.response_version} ·{" "}
              {(
                {
                  queued: "等待执行",
                  running: "执行中",
                  succeeded: "已确认生效",
                  failed: "失败",
                  interrupted: "执行中断",
                } as Record<string, string>
              )[latest.status] ?? latest.status}
              {latest.result?.error && `：${latest.result.error}`}
            </p>
          )}
        </fieldset>
      )}
    </section>
  );
}
