import { loader } from "@monaco-editor/react";
import * as monaco from "monaco-editor/esm/vs/editor/editor.api";
import EditorWorker from "monaco-editor/esm/vs/editor/editor.worker?worker";

// Load Monaco locally only when an editor or diff view is opened.
self.MonacoEnvironment = { getWorker: () => new EditorWorker() };
loader.config({ monaco });
