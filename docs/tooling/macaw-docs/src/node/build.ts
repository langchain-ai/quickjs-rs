import { existsSync } from "node:fs";
import { mkdir, readFile, rm, stat, writeFile } from "node:fs/promises";
import { builtinModules } from "node:module";
import path from "node:path";
import { pathToFileURL } from "node:url";
import {
  build as viteBuild,
  createServer,
  mergeConfig,
  preview as vitePreview,
  type InlineConfig,
  type Manifest,
  type Plugin,
} from "vite";
import { normalizeBase, type DocsConfig, type ResolvedDocsConfig, type SiteConfig } from "../config.ts";
import { documentTitle, modulePreloadTag, renderDocument, stylesheetTag } from "./html.ts";
import { scanContent } from "./content.ts";
import { CLIENT_ENTRY, PACKAGE_DIR, SERVER_ENTRY, macawDocs } from "./plugin.ts";

const CONFIG_FILES = ["macaw-docs.config.ts", "macaw-docs.config.mjs", "macaw-docs.config.js"];

/** Packages that must be bundled into the SSR build (ESM-only and import CSS). */
const SSR_NO_EXTERNAL = [/^@langchain\/macaw-components/];

/** React runtime entries pre-bundled for dev. */
const OPTIMIZE_INCLUDE = [
  "react",
  "react/jsx-runtime",
  "react/jsx-dev-runtime",
  "react-dom",
  "react-dom/client",
  "@mdx-js/react",
];

async function loadConfig(root: string): Promise<ResolvedDocsConfig> {
  const absRoot = path.resolve(root);
  const configFile = CONFIG_FILES.map((name) => path.join(absRoot, name)).find((file) => existsSync(file));
  if (!configFile) {
    throw new Error(`No macaw-docs config found in ${absRoot}. Create one of: ${CONFIG_FILES.join(", ")}.`);
  }
  const mod = (await import(`${pathToFileURL(configFile).href}?t=${Date.now()}`)) as { default?: DocsConfig };
  const user = mod.default;
  if (!user || typeof user !== "object") throw new Error(`${configFile} must default-export a config object.`);
  if (typeof user.title !== "string" || !user.title.trim()) throw new Error(`${configFile}: "title" is required.`);
  const dir = path.dirname(configFile);
  return {
    root: dir,
    configFile,
    title: user.title,
    tagline: user.tagline ?? "",
    description: user.description ?? "",
    logo: user.logo,
    contentDir: path.resolve(dir, user.content ?? "docs"),
    outDir: path.resolve(dir, user.outDir ?? "dist"),
    base: normalizeBase(user.base),
    links: user.links ?? [],
    theme: user.theme ?? "system",
    vite: user.vite ?? {},
  };
}

function isInside(file: string, dir: string): boolean {
  const rel = path.relative(dir, file);
  return !rel.startsWith("..") && !path.isAbsolute(rel);
}

function baseConfig(config: ResolvedDocsConfig): InlineConfig {
  // Sites outside this package resolve the React runtime through it.
  const include = isInside(config.root, PACKAGE_DIR)
    ? OPTIMIZE_INCLUDE
    : OPTIMIZE_INCLUDE.map((id) => `macaw-docs > ${id}`);
  const shared: InlineConfig = {
    configFile: false,
    root: config.root,
    base: config.base,
    appType: "custom",
    plugins: [macawDocs(config)],
    server: { fs: { allow: [config.root, PACKAGE_DIR] } },
    optimizeDeps: { entries: [CLIENT_ENTRY], include },
    build: {
      rollupOptions: {
        // Radix and friends ship "use client" banners, which mean nothing in a static build.
        onwarn(warning, warn) {
          if (warning.code === "MODULE_LEVEL_DIRECTIVE") return;
          warn(warning);
        },
      },
    },
  };
  return mergeConfig(shared, config.vite);
}

export interface DevOptions {
  port?: number;
  host?: boolean | string;
}

export async function dev(root: string, options: DevOptions = {}) {
  const config = await loadConfig(root);
  const server = await createServer(
    mergeConfig(baseConfig(config), { server: { port: options.port, host: options.host } } satisfies InlineConfig),
  );
  await server.listen();
  server.printUrls();
  return server;
}

/**
 * Leaves bare imports from `node_modules` external but rewrites them to
 * absolute paths, so the server bundle loads them from wherever the package
 * manager placed them rather than from the output directory.
 */
function externalizeToAbsolute(): Plugin {
  const builtins = new Set(builtinModules);
  return {
    name: "macaw-docs:ssr-externals",
    enforce: "pre",
    async resolveId(id, importer, options) {
      if (!importer || id.startsWith("\0") || id.startsWith(".") || path.isAbsolute(id)) return null;
      if (id.startsWith("node:") || builtins.has(id) || id.startsWith("virtual:")) return null;
      if (SSR_NO_EXTERNAL.some((pattern) => pattern.test(id))) return null;
      const resolved = await this.resolve(id, importer, { ...options, skipSelf: true });
      if (!resolved || resolved.external || !resolved.id.includes(`${path.sep}node_modules${path.sep}`)) return null;
      if (!/\.(?:c|m)?js$/.test(resolved.id)) return null;
      return { id: resolved.id, external: "absolute" };
    },
  };
}

interface ServerModule {
  site: SiteConfig;
  routes: () => string[];
  render: (route: string) => Promise<{
    html: string;
    page?: { title: string; description: string };
    status: number;
  }>;
}

function collectAssets(manifest: Manifest, key: string, seen: Set<string>): { css: string[]; preload: string[] } {
  const css: string[] = [];
  const preload: string[] = [];
  const visit = (k: string) => {
    if (seen.has(k)) return;
    seen.add(k);
    const chunk = manifest[k];
    if (!chunk) return;
    preload.push(chunk.file);
    css.push(...(chunk.css ?? []));
    for (const child of chunk.imports ?? []) visit(child);
  };
  visit(key);
  return { css, preload };
}

function routeFile(outDir: string, route: string): string {
  return route === "/" ? path.join(outDir, "index.html") : path.join(outDir, route.slice(1), "index.html");
}

export async function build(root: string): Promise<{ outDir: string; routes: string[] }> {
  const started = performance.now();
  process.env.NODE_ENV = "production";
  const config = await loadConfig(root);
  const serverDir = path.join(config.outDir, ".server");

  await viteBuild(
    mergeConfig(baseConfig(config), {
      logLevel: "warn",
      build: {
        chunkSizeWarningLimit: 2048,
        outDir: config.outDir,
        emptyOutDir: true,
        manifest: true,
        rollupOptions: { input: CLIENT_ENTRY },
      },
    } satisfies InlineConfig),
  );

  await viteBuild(
    mergeConfig(baseConfig(config), {
      logLevel: "warn",
      plugins: [externalizeToAbsolute()],
      ssr: { noExternal: SSR_NO_EXTERNAL },
      build: {
        ssr: SERVER_ENTRY,
        outDir: serverDir,
        emptyOutDir: true,
        minify: false,
        copyPublicDir: false,
        rollupOptions: { output: { entryFileNames: "entry-server.mjs" } },
      },
    } satisfies InlineConfig),
  );

  const manifestFile = path.join(config.outDir, ".vite/manifest.json");
  const manifest = JSON.parse(await readFile(manifestFile, "utf8")) as Manifest;
  const entryKey = Object.keys(manifest).find(
    (key) => manifest[key]?.isEntry && key.replace(/\\/g, "/").endsWith("src/client/entry-client.tsx"),
  );
  if (!entryKey) throw new Error("Client entry missing from the Vite manifest.");
  const entryChunk = manifest[entryKey];
  if (!entryChunk) throw new Error("Client entry missing from the Vite manifest.");
  const sharedSeen = new Set<string>();
  const shared = collectAssets(manifest, entryKey, sharedSeen);
  const asset = (file: string) => config.base + file;

  const server = (await import(pathToFileURL(path.join(serverDir, "entry-server.mjs")).href)) as ServerModule;
  const pageKeys = new Map<string, string>();
  for (const [key, chunk] of Object.entries(manifest)) {
    if (chunk.isDynamicEntry && chunk.src) pageKeys.set(path.resolve(config.root, chunk.src), key);
  }
  const scan = await scanContent(config.contentDir);

  const write = async (route: string, file: string) => {
    const result = await server.render(route);
    const page = scan.pages.find((p) => p.route === route);
    const pageKey = page ? pageKeys.get(page.file) : undefined;
    const pageAssets = pageKey ? collectAssets(manifest, pageKey, new Set(sharedSeen)) : { css: [], preload: [] };
    const css = [...shared.css, ...pageAssets.css];
    const preload = [...shared.preload.slice(1), ...pageAssets.preload];
    const head = [...css.map((f) => stylesheetTag(asset(f))), ...preload.map((f) => modulePreloadTag(asset(f)))].join(
      "\n    ",
    );
    const html = await renderDocument({
      title: documentTitle(
        result.page?.title ?? (result.status === 404 ? "Page not found" : undefined),
        server.site.title,
      ),
      description: result.page?.description || server.site.description,
      theme: server.site.theme,
      head,
      body: result.html,
      scripts: [asset(entryChunk.file)],
    });
    await mkdir(path.dirname(file), { recursive: true });
    await writeFile(file, html);
  };

  const routes = server.routes();
  for (const route of routes) await write(route, routeFile(config.outDir, route));
  await write("/__macaw-docs-not-found__", path.join(config.outDir, "404.html"));

  await rm(serverDir, { recursive: true, force: true });
  await rm(path.join(config.outDir, ".vite"), { recursive: true, force: true });

  const seconds = ((performance.now() - started) / 1000).toFixed(1);
  console.log(
    `macaw-docs: built ${routes.length} page${routes.length === 1 ? "" : "s"} + 404 to ${path.relative(process.cwd(), config.outDir) || "."} in ${seconds}s`,
  );
  return { outDir: config.outDir, routes };
}

/** Serves the built site like a static host: `/page` → `/page/index.html`, 404s → `404.html`. */
function staticHost(outDir: string, base: string): Plugin {
  const fileFor = async (url: string): Promise<string | undefined> => {
    let pathname: string;
    try {
      pathname = decodeURIComponent(new URL(url, "http://localhost").pathname);
    } catch {
      return undefined;
    }
    if (!pathname.startsWith(base)) return undefined;
    const rel = pathname.slice(base.length);
    if (path.extname(rel)) return undefined;
    const candidate = path.join(outDir, rel, "index.html");
    if (!isInside(candidate, outDir)) return undefined;
    const dir = rel.replace(/\/+$/, "");
    try {
      return (await stat(candidate)).isFile() ? `${base}${dir ? `${dir}/` : ""}index.html` : undefined;
    } catch {
      return undefined;
    }
  };
  return {
    name: "macaw-docs:static-host",
    configurePreviewServer(server) {
      server.middlewares.use(async (req, _res, next) => {
        const rewritten = req.url ? await fileFor(req.url) : undefined;
        if (rewritten) req.url = rewritten + (req.url?.match(/\?.*$/)?.[0] ?? "");
        next();
      });
      return () => {
        server.middlewares.use(async (_req, res) => {
          const notFound = path.join(outDir, "404.html");
          res.statusCode = 404;
          res.setHeader("Content-Type", "text/html");
          res.end(existsSync(notFound) ? await readFile(notFound) : "Not found");
        });
      };
    },
  };
}

export async function preview(root: string, options: DevOptions = {}) {
  const config = await loadConfig(root);
  if (!existsSync(config.outDir)) throw new Error(`No build found at ${config.outDir}. Run "macaw-docs build" first.`);
  const server = await vitePreview({
    configFile: false,
    root: config.root,
    base: config.base,
    appType: "mpa",
    plugins: [staticHost(config.outDir, config.base)],
    build: { outDir: config.outDir },
    preview: { port: options.port, host: options.host },
  });
  server.printUrls();
  return server;
}
