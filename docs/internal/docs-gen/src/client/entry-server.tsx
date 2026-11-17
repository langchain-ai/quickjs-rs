import { renderToString } from "react-dom/server";
import { pages, site } from "virtual:macaw-docs/site";
import { Root } from "./App.tsx";
import { loadPage } from "./router.ts";

export { site };

export function routes(): string[] {
  return pages.map((page) => page.path);
}

export interface RenderResult {
  html: string;
  page?: { title: string; description: string };
  status: number;
}

export async function render(route: string): Promise<RenderResult> {
  const page = await loadPage(route);
  const html = renderToString(<Root route={route} page={page} />);
  if (!page) return { html, status: 404 };
  return {
    html,
    page: { title: page.entry.title, description: page.entry.description },
    status: 200,
  };
}
