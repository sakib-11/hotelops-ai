import React from "react";
import ReactDOM from "react-dom/client";
import App from "./app/App";
import { QueryProvider } from "@/query";
import "@/design-system/tokens.css";

const rootElement = document.getElementById("root");

if (!rootElement) {
  throw new Error("Root element not found");
}

ReactDOM.createRoot(rootElement).render(
  <React.StrictMode>
    <QueryProvider>
      <App />
    </QueryProvider>
  </React.StrictMode>,
);
