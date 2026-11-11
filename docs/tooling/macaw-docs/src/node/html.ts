import type { ThemeMode } from "../config.ts";

function escapeHtml(value: string): string {
  return value
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

/** `Page · Site`, or just `Site` when the two are equal or there is no page. */
export function documentTitle(pageTitle: string | undefined, siteTitle: string): string {
  if (!pageTitle || pageTitle === siteTitle) return siteTitle;
  return `${pageTitle} · ${siteTitle}`;
}

// A brand-neutral document glyph; favicons cannot read the page's tokens.
const FAVICON_SVG =
  '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">' +
  "<style>rect{fill:#3f3f46}path{stroke:#fafafa}@media (prefers-color-scheme:dark){rect{fill:#e4e4e7}path{stroke:#18181b}}</style>" +
  '<rect width="32" height="32" rx="7"/>' +
  '<path d="M10 11h12M10 16h12M10 21h7" stroke-width="2.5" stroke-linecap="round" fill="none"/>' +
  "</svg>";

const FAVICON_HREF = `data:image/svg+xml,${encodeURIComponent(FAVICON_SVG)}`;

/**
 * Inline script that mirrors Macaw's `applyThemeToDocument` before the
 * stylesheet loads, so the first paint uses the stored theme.
 */
async function themeScript(defaultMode: ThemeMode): Promise<string> {
  // Imported lazily: the module imports React, and loading React before Vite
  // sets NODE_ENV would pin its development build next to a production
  // react-dom/server during prerendering.
  const { PREFERS_DARK_QUERY, THEME_MODE_STORAGE_KEY } =
    await import("@langchain/macaw-components/hooks/useColorScheme");
  const key = JSON.stringify(THEME_MODE_STORAGE_KEY);
  const query = JSON.stringify(PREFERS_DARK_QUERY);
  const fallback = JSON.stringify(defaultMode);
  return (
    `(function(){var m=null;try{m=localStorage.getItem(${key})}catch(e){}` +
    `if(m!=="light"&&m!=="dark"&&m!=="system")m=${fallback};` +
    `var r=m;if(m==="system"){try{r=matchMedia(${query}).matches?"dark":"light"}catch(e){r="light"}}` +
    `var d=document.documentElement;d.classList.toggle("dark",r==="dark");` +
    `d.dataset.theme=r;d.dataset.themeMode=m;d.style.colorScheme=r})();`
  );
}

export interface DocumentOptions {
  title: string;
  description: string;
  theme: ThemeMode;
  /** Extra `<head>` markup: stylesheets and module preloads. */
  head?: string;
  /** Pre-rendered app markup. */
  body?: string;
  /** Module script URLs. */
  scripts: string[];
}

export function stylesheetTag(href: string): string {
  return `<link rel="stylesheet" href="${escapeHtml(href)}">`;
}

export function modulePreloadTag(href: string): string {
  return `<link rel="modulepreload" crossorigin href="${escapeHtml(href)}">`;
}

export async function renderDocument(options: DocumentOptions): Promise<string> {
  const description = options.description
    ? `\n    <meta name="description" content="${escapeHtml(options.description)}">`
    : "";
  const scripts = options.scripts
    .map((src) => `<script type="module" crossorigin src="${escapeHtml(src)}"></script>`)
    .join("\n    ");
  return `<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>${escapeHtml(options.title)}</title>${description}
    <link rel="icon" type="image/svg+xml" href="${FAVICON_HREF}">
    <script>${await themeScript(options.theme)}</script>
    ${options.head ?? ""}
  </head>
  <body>
    <div id="app">${options.body ?? ""}</div>
    ${scripts}
  </body>
</html>
`;
}
