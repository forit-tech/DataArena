import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { App } from "./app/App";
import "./styles/tokens.css";
import "./styles/base.css";
import "./styles/table.css";
import "./styles/sql.css";
import "./styles/health.css";

const container = document.getElementById("root");

if (!container) {
  throw new Error("Не найден корневой элемент #root.");
}

createRoot(container).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
