import { useEffect, useRef } from "react";
import "./monacoSetup";
import { editor } from "monaco-editor/esm/vs/editor/editor.api";

export default function ConfigDiff({
  original,
  modified,
}: {
  original: string;
  modified: string;
}) {
  const host = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!host.current) return;
    const left = editor.createModel(original, "plaintext");
    const right = editor.createModel(modified, "plaintext");
    const widget = editor.createDiffEditor(host.current, {
      theme: "vs-dark",
      readOnly: true,
      automaticLayout: true,
      renderSideBySide: true,
      minimap: { enabled: false },
    });
    widget.setModel({ original: left, modified: right });
    return () => {
      // Detach before disposal: Monaco diff computations may still refer to the models.
      widget.setModel(null);
      widget.dispose();
      left.dispose();
      right.dispose();
    };
  }, [original, modified]);
  return <div ref={host} style={{ height: "65vh" }} />;
}
