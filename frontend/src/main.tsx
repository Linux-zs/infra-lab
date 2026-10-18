import React from "react";
import ReactDOM from "react-dom/client";
import { ReactFlowProvider } from "@xyflow/react";
import App from "./App";
import "./style.css";
import "@fontsource/ibm-plex-mono/400.css";
import "@fontsource/ibm-plex-mono/500.css";
ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <ReactFlowProvider>
      <App />
    </ReactFlowProvider>
  </React.StrictMode>,
);
