import { useEffect, useRef, useState } from "react";
import { api, type Lab } from "./types";
import type { ScenarioTemplate } from "./ScenarioPanel";

export default function ScenarioCreate({
  close,
  created,
}: {
  close: () => void;
  created: (lab: Lab) => void;
}) {
  const dialog = useRef<HTMLDialogElement>(null);
  const requestId = useRef(crypto.randomUUID());
  const [templates, setTemplates] = useState<ScenarioTemplate[]>([]);
  const [selected, setSelected] = useState("");
  const [name, setName] = useState("");
  const [working, setWorking] = useState(false);
  const [error, setError] = useState("");
  useEffect(() => {
    dialog.current?.showModal();
    let active = true;
    api<ScenarioTemplate[]>("/scenarios")
      .then((items) => {
        if (active) {
          setTemplates(items);
          setSelected(items[0]?.id || "");
          setName(items[0]?.title || "");
        }
      })
      .catch((e) => {
        if (active) setError(String(e));
      });
    return () => {
      active = false;
    };
  }, []);
  async function create() {
    setWorking(true);
    setError("");
    try {
      created(
        await api<Lab>(`/scenarios/${selected}/labs`, "POST", {
          name: name.trim(),
          request_id: requestId.current,
        }),
      );
    } catch (e) {
      setError(String(e));
    } finally {
      setWorking(false);
    }
  }
  return (
    <dialog
      ref={dialog}
      className="scenario-create"
      aria-labelledby="scenario-create-title"
      onCancel={(e) => {
        e.preventDefault();
        if (!working) close();
      }}
    >
      <div className="scenario-create-heading">
        <h2 id="scenario-create-title">从场景新建实验</h2>
        <button aria-label="关闭场景选择" disabled={working} onClick={close}>
          关闭
        </button>
      </div>
      {error && (
        <p className="error" role="alert">
          {error}
        </p>
      )}
      {!templates.length && !error && <p role="status">正在读取场景…</p>}
      <fieldset className="scenario-choices" disabled={working}>
        <legend>选择场景</legend>
        {templates.map((template) => (
          <label
            key={template.id}
            className={selected === template.id ? "active" : ""}
          >
            <input
              type="radio"
              name="scenario-template"
              value={template.id}
              checked={selected === template.id}
              onChange={() => {
                setSelected(template.id);
                setName(template.title);
                requestId.current = crypto.randomUUID();
              }}
            />
            <strong>{template.title}</strong>
            <span>
              {template.node_count} 节点 · {template.memory_mb} MB
            </span>
            <span>{template.metrics.join(" / ")}</span>
          </label>
        ))}
      </fieldset>
      <label className="full-field">
        实验名称
        <input
          aria-label="场景实验名称"
          value={name}
          maxLength={80}
          disabled={working}
          onChange={(e) => {
            setName(e.target.value);
            requestId.current = crypto.randomUUID();
          }}
        />
      </label>
      <div className="scenario-create-actions">
        <span>创建后保存为草稿，点击“应用拓扑”部署。</span>
        <button
          className="primary"
          disabled={working || !selected || !name.trim()}
          onClick={() => void create()}
        >
          {working ? "正在创建…" : "创建草稿"}
        </button>
      </div>
    </dialog>
  );
}
