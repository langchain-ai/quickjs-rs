import "virtual:macaw-docs/styles.css";
import { createRoot, hydrateRoot } from "react-dom/client";
import { Root } from "./App.tsx";
import { loadPage, toRoute } from "./router.ts";

// Load the current page before hydrating so the first client render matches
// the static HTML. No top-level await: default build targets may reject it.
async function start(): Promise<void> {
  const container = document.getElementById("app");
  if (!container) throw new Error("macaw-docs: #app container missing.");
  const route = toRoute(window.location.pathname);
  const page = await loadPage(route);
  const app = <Root route={route} page={page} />;
  if (container.hasChildNodes()) hydrateRoot(container, app);
  else createRoot(container).render(app);
}

void start();
