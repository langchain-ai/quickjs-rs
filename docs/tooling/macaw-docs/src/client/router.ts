import { pages, site } from "virtual:macaw-docs/site";
import type { LoadedPage, PageEntry } from "./types.ts";

const byPath = new Map<string, PageEntry>(pages.map((page) => [page.path, page]));

/** Converts a browser pathname to a content route: `/base/guides/` → `/guides`. */
export function toRoute(pathname: string): string {
  let route = pathname;
  const base = site.base;
  if (base !== "/") {
    if (route === base.slice(0, -1)) route = "/";
    else if (route.startsWith(base)) route = `/${route.slice(base.length)}`;
  }
  route = route.replace(/\/index\.html$/, "/");
  if (route.length > 1) route = route.replace(/\/+$/, "");
  try {
    route = decodeURIComponent(route);
  } catch {
    // Keep the raw route when it is not valid percent-encoding.
  }
  return route || "/";
}

/** Converts a content route to an href under the configured base. */
export function toHref(route: string): string {
  return route === "/" ? site.base : `${site.base}${route.slice(1)}`;
}

export function findPage(route: string): PageEntry | undefined {
  return byPath.get(route);
}

export async function loadPage(route: string): Promise<LoadedPage | null> {
  const entry = findPage(route);
  if (!entry) return null;
  return { entry, module: await entry.load() };
}
