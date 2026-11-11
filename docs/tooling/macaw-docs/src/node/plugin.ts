import path from "node:path";
import mdx from "@mdx-js/rollup";
import tailwindcss from "@tailwindcss/vite";
import remarkGfm from "remark-gfm";
import type { Plugin, PluginOption, ViteDevServer } from "vite";
import { toSiteConfig, type ResolvedDocsConfig } from "../config.ts";
import { isMetaFile, isPageFile, scanContent, splitFrontmatter, type ContentScan } from "./content.ts";
import { documentTitle, renderDocument } from "./html.ts";
import { remarkDocs } from "./remark.ts";

export const PACKAGE_DIR = path.resolve(import.meta.dirname, "../..");
const CLIENT_DIR = path.join(PACKAGE_DIR, "src/client");
export const CLIENT_ENTRY = path.join(CLIENT_DIR, "entry-client.tsx");
export const SERVER_ENTRY = path.join(CLIENT_DIR, "entry-server.tsx");
const STYLES_SOURCE = path.join(CLIENT_DIR, "styles.css");

const SITE_ID = "virtual:macaw-docs/site";
const RESOLVED_SITE_ID = `\0${SITE_ID}`;
const STYLES_ID = "virtual:macaw-docs/styles.css";
// @tailwindcss/vite skips `\0` ids, so the generated stylesheet gets a
// real-looking absolute path that never exists on disk.
const STYLES_BASENAME = "__macaw-docs-styles.css";
const STYLES_FILE = path.join(CLIENT_DIR, STYLES_BASENAME);

/** Imports that pages outside the package must resolve to the package's copies. */
const SHARED_IMPORT = /^(?:react|react-dom|@mdx-js\/react|@langchain\/macaw-components)(?:\/|$)/;

function cleanUrl(id: string): string {
  return id.replace(/[?#].*$/, "");
}

function isInside(file: string, dir: string): boolean {
  const rel = path.relative(dir, file);
  return rel !== "" && !rel.startsWith("..") && !path.isAbsolute(rel);
}

function cssString(value: string): string {
  return JSON.stringify(value.split(path.sep).join("/"));
}

export function macawDocs(config: ResolvedDocsConfig): PluginOption[] {
  const frontmatter = new Map<string, Record<string, unknown>>();
  let scan: Promise<ContentScan> | null = null;
  let siteCode: string | null = null;
  let server: ViteDevServer | undefined;

  const isPage = (file: string) => isPageFile(file) && isInside(file, config.contentDir);
  const getScan = () => (scan ??= scanContent(config.contentDir));

  async function generateSiteModule(): Promise<{ code: string; data: ContentScan }> {
    const data = await getScan();
    const pages = data.pages
      .map(
        (page) =>
          `  { path: ${JSON.stringify(page.route)}, title: ${JSON.stringify(page.title)}, ` +
          `description: ${JSON.stringify(page.description)}, load: () => import(${JSON.stringify(page.file)}) },`,
      )
      .join("\n");
    let code =
      `export const site = ${JSON.stringify(toSiteConfig(config))};\n` +
      `export const nav = ${JSON.stringify(data.nav)};\n` +
      `export const pages = [\n${pages}\n];\n`;
    if (server) {
      // Page body edits swap the rendered component without a reload.
      const files = JSON.stringify(data.pages.map((page) => page.file));
      const routes = JSON.stringify(data.pages.map((page) => page.route));
      code +=
        `if (import.meta.hot) {\n` +
        `  const routes = ${routes};\n` +
        `  import.meta.hot.accept(${files}, (modules) => {\n` +
        `    modules.forEach((module, i) => {\n` +
        `      if (module) window.dispatchEvent(new CustomEvent("macaw-docs:page-update", { detail: { path: routes[i], module } }));\n` +
        `    });\n` +
        `  });\n` +
        `}\n`;
    }
    siteCode = code;
    return { code, data };
  }

  function reloadSite(dev: ViteDevServer): void {
    scan = null;
    const graph = dev.environments.client.moduleGraph;
    const mod = graph.getModuleById(RESOLVED_SITE_ID);
    if (mod) graph.invalidateModule(mod);
    dev.ws.send({ type: "full-reload" });
  }

  const core: Plugin = {
    name: "macaw-docs",
    enforce: "pre",

    async resolveId(id, importer, options) {
      if (id === SITE_ID) return RESOLVED_SITE_ID;
      const clean = cleanUrl(id);
      if (id === STYLES_ID) return STYLES_FILE;
      if (clean.endsWith(`/${STYLES_BASENAME}`)) return STYLES_FILE + id.slice(clean.length);
      if (importer && SHARED_IMPORT.test(id) && isPage(cleanUrl(importer))) {
        return this.resolve(id, CLIENT_ENTRY, { ...options, skipSelf: true });
      }
      return null;
    },

    async load(id) {
      if (id === RESOLVED_SITE_ID) {
        const { code, data } = await generateSiteModule();
        for (const page of data.pages) this.addWatchFile(page.file);
        return code;
      }
      if (cleanUrl(id) === STYLES_FILE) {
        return [
          `@import ${cssString(STYLES_SOURCE)};`,
          `@source ${cssString(CLIENT_DIR)};`,
          `@source ${cssString(path.join(PACKAGE_DIR, "src/components"))};`,
          `@source ${cssString(config.contentDir)};`,
          "",
        ].join("\n");
      }
      return null;
    },

    transform(code, id) {
      const file = cleanUrl(id);
      if (!isPage(file)) return null;
      const { data, body } = splitFrontmatter(code);
      frontmatter.set(file, data);
      return { code: body, map: null };
    },

    configureServer(dev) {
      server = dev;
      dev.watcher.add(config.contentDir);
      dev.watcher.on("all", (event, file) => {
        if (!isInside(file, config.contentDir)) return;
        const structural = event === "add" || event === "unlink" || event === "unlinkDir";
        if (
          (structural && (isPageFile(file) || isMetaFile(file) || event === "unlinkDir")) ||
          (event === "change" && isMetaFile(file))
        ) {
          reloadSite(dev);
        }
      });

      return () => {
        dev.middlewares.use(async (req, res, next) => {
          if (req.method !== "GET" || !req.headers.accept?.includes("text/html")) return next();
          try {
            const site = toSiteConfig(config);
            const html = await renderDocument({
              title: documentTitle(undefined, site.title),
              description: site.description,
              theme: site.theme,
              scripts: [path.posix.join(config.base, "@fs", CLIENT_ENTRY.split(path.sep).join("/"))],
            });
            res.statusCode = 200;
            res.setHeader("Content-Type", "text/html");
            res.end(await dev.transformIndexHtml(req.url ?? "/", html));
          } catch (error) {
            next(error);
          }
        });
      };
    },

    async hotUpdate({ file }) {
      if (this.environment.name !== "client" || !server || !isPage(file)) return;
      const before = siteCode;
      scan = null;
      const { code } = await generateSiteModule();
      if (code !== before) {
        // Titles or ordering moved: the nav needs a fresh site module.
        reloadSite(server);
        return [];
      }
      return undefined;
    },
  };

  const mdxPlugin = mdx({
    providerImportSource: "@mdx-js/react",
    remarkPlugins: [remarkGfm, [remarkDocs, { frontmatter }]],
  }) as Plugin;

  return [core, { enforce: "pre", ...mdxPlugin }, tailwindcss()];
}
