import { existsSync } from "node:fs";
import { readdir, readFile } from "node:fs/promises";
import path from "node:path";
import { pathToFileURL } from "node:url";
import { parse as parseYaml } from "yaml";
import type { Frontmatter, NavItem } from "../client/types.ts";

const PAGE_EXTENSIONS = [".md", ".mdx"];
const META_FILES = ["meta.ts", "meta.js", "meta.json"];

/** A page discovered in the content directory. */
interface ContentPage {
  /** Absolute file path. */
  file: string;
  /** Path relative to the content directory, with forward slashes. */
  relPath: string;
  route: string;
  title: string;
  description: string;
  frontmatter: Frontmatter;
}

/** Folder metadata from `meta.ts`, `meta.js`, or `meta.json`. */
interface FolderMeta {
  title?: string;
  order?: string[];
  collapsed?: boolean;
}

export interface ContentScan {
  pages: ContentPage[];
  nav: NavItem[];
}

export function isPageFile(file: string): boolean {
  return PAGE_EXTENSIONS.includes(path.extname(file));
}

export function isMetaFile(file: string): boolean {
  return META_FILES.includes(path.basename(file));
}

function isIgnored(name: string): boolean {
  return name.startsWith("_") || name.startsWith(".");
}

/** Maps a content-relative file path to its route: `guides/index.mdx` → `/guides`. */
export function routeFor(relPath: string): string {
  const segments = relPath
    .split(/[\\/]/)
    .filter(Boolean)
    .map((segment, i, all) => (i === all.length - 1 ? stripExtension(segment) : segment));
  if (segments.at(-1) === "index") segments.pop();
  return `/${segments.join("/")}`;
}

function stripExtension(name: string): string {
  const ext = path.extname(name);
  return ext ? name.slice(0, -ext.length) : name;
}

const FRONTMATTER_RE = /^---[ \t]*\r?\n([\s\S]*?)\r?\n---[ \t]*(?:\r?\n|$)/;

/**
 * Splits YAML frontmatter from a page source. `body` replaces the frontmatter
 * with the same number of newlines so line numbers in errors stay correct.
 */
export function splitFrontmatter(source: string): { data: Frontmatter; body: string } {
  const match = FRONTMATTER_RE.exec(source);
  if (!match) return { data: {}, body: source };
  const parsed: unknown = parseYaml(match[1] ?? "");
  const data = parsed && typeof parsed === "object" && !Array.isArray(parsed) ? (parsed as Frontmatter) : {};
  const lines = match[0].split("\n").length - 1;
  return { data, body: "\n".repeat(lines) + source.slice(match[0].length) };
}

const MINOR_WORDS = new Set([
  "a",
  "an",
  "the",
  "and",
  "or",
  "for",
  "to",
  "of",
  "in",
  "on",
  "at",
  "by",
  "with",
  "from",
  "as",
]);

/** Title Case for names derived from file names: `deploy-to-prod` → `Deploy to Prod`. */
function titleCase(name: string): string {
  const words = name.split(/[-_\s]+/).filter(Boolean);
  return words
    .map((word, i) => {
      const lower = word.toLowerCase();
      if (i > 0 && i < words.length - 1 && MINOR_WORDS.has(lower)) return lower;
      return word.charAt(0).toUpperCase() + word.slice(1);
    })
    .join(" ");
}

/** First `# heading` outside fenced code blocks. */
function firstHeading(body: string): string | undefined {
  let fence: string | null = null;
  for (const line of body.split(/\r?\n/)) {
    const fenceMatch = /^\s{0,3}(`{3,}|~{3,})/.exec(line);
    if (fenceMatch) {
      const marker = fenceMatch[1] ?? "";
      if (fence === null) fence = marker;
      else if (marker[0] === fence[0] && marker.length >= fence.length) fence = null;
      continue;
    }
    if (fence !== null) continue;
    const heading = /^\s{0,3}#[ \t]+(.+?)[ \t#]*$/.exec(line);
    if (heading?.[1]) return stripInlineMarkdown(heading[1]);
  }
  return undefined;
}

function stripInlineMarkdown(text: string): string {
  return text
    .replace(/!?\[([^\]]*)\]\([^)]*\)/g, "$1")
    .replace(/[`*_~]/g, "")
    .trim();
}

/** Title fallback: frontmatter `title` → first `# heading` → title-cased file name. */
function pageTitle(relPath: string, data: Frontmatter, body: string): string {
  if (typeof data.title === "string" && data.title.trim()) return data.title.trim();
  const heading = firstHeading(body);
  if (heading) return heading;
  const name = stripExtension(path.basename(relPath));
  return name === "index" ? "Overview" : titleCase(name);
}

async function loadFolderMeta(dir: string): Promise<FolderMeta> {
  for (const name of META_FILES) {
    const file = path.join(dir, name);
    if (!existsSync(file)) continue;
    let value: unknown;
    if (name.endsWith(".json")) {
      value = JSON.parse(await readFile(file, "utf8"));
    } else {
      const mod = (await import(`${pathToFileURL(file).href}?t=${Date.now()}`)) as { default?: unknown };
      value = mod.default;
    }
    if (!value || typeof value !== "object") throw new Error(`${file} must export an object.`);
    return value as FolderMeta;
  }
  return {};
}

interface DirNode {
  dir: string;
  rel: string;
  meta: FolderMeta;
  index: ContentPage | undefined;
  pages: ContentPage[];
  dirs: DirNode[];
}

async function walk(dir: string, rel: string, pages: ContentPage[]): Promise<DirNode> {
  const node: DirNode = { dir, rel, meta: await loadFolderMeta(dir), index: undefined, pages: [], dirs: [] };
  const entries = await readdir(dir, { withFileTypes: true });
  entries.sort((a, b) => a.name.localeCompare(b.name));
  for (const entry of entries) {
    if (isIgnored(entry.name)) continue;
    const childRel = rel ? `${rel}/${entry.name}` : entry.name;
    const childPath = path.join(dir, entry.name);
    if (entry.isDirectory()) {
      node.dirs.push(await walk(childPath, childRel, pages));
    } else if (entry.isFile() && isPageFile(entry.name)) {
      const source = await readFile(childPath, "utf8");
      const { data, body } = splitFrontmatter(source);
      const page: ContentPage = {
        file: childPath,
        relPath: childRel,
        route: routeFor(childRel),
        title: pageTitle(childRel, data, body),
        description: typeof data.description === "string" ? data.description : "",
        frontmatter: data,
      };
      pages.push(page);
      if (stripExtension(entry.name) === "index") node.index = page;
      else node.pages.push(page);
    }
  }
  return node;
}

interface Sortable {
  slug: string;
  title: string;
  order: number | undefined;
  rootIndex: boolean;
  item: NavItem;
}

function compareSortable(order: string[] | undefined) {
  const position = (slug: string) => {
    const i = order?.indexOf(slug) ?? -1;
    return i === -1 ? Number.POSITIVE_INFINITY : i;
  };
  return (a: Sortable, b: Sortable): number => {
    const pa = position(a.slug);
    const pb = position(b.slug);
    if (pa !== pb) return pa < pb ? -1 : 1;
    if (a.rootIndex !== b.rootIndex) return a.rootIndex ? -1 : 1;
    const ao = a.order ?? Number.POSITIVE_INFINITY;
    const bo = b.order ?? Number.POSITIVE_INFINITY;
    if (ao !== bo) return ao < bo ? -1 : 1;
    return a.title.localeCompare(b.title);
  };
}

function navLabel(page: ContentPage): string {
  const label = page.frontmatter.sidebar?.label;
  return typeof label === "string" && label.trim() ? label.trim() : page.title;
}

function buildNav(node: DirNode, isRoot: boolean): NavItem[] {
  const entries: Sortable[] = [];
  const pages = isRoot && node.index ? [node.index, ...node.pages] : node.pages;
  for (const page of pages) {
    if (page.frontmatter.sidebar?.hidden === true) continue;
    const title = navLabel(page);
    entries.push({
      slug: page === node.index ? "index" : stripExtension(path.basename(page.relPath)),
      title,
      order: page.frontmatter.sidebar?.order,
      rootIndex: page === node.index,
      item: { type: "page", title, path: page.route },
    });
  }
  for (const dir of node.dirs) {
    const items = buildNav(dir, false);
    const index = dir.index && dir.index.frontmatter.sidebar?.hidden !== true ? dir.index : undefined;
    if (items.length === 0 && !index) continue;
    const title = dir.meta.title ?? (index ? navLabel(index) : titleCase(path.basename(dir.dir)));
    const group: NavItem = {
      type: "group",
      title,
      ...(index ? { path: index.route } : {}),
      collapsed: dir.meta.collapsed === true,
      items,
    };
    entries.push({
      slug: path.basename(dir.dir),
      title,
      order: index?.frontmatter.sidebar?.order,
      rootIndex: false,
      item: group,
    });
  }
  entries.sort(compareSortable(node.meta.order));
  return entries.map((entry) => entry.item);
}

/** Scans the content directory for pages and builds the navigation tree. */
export async function scanContent(contentDir: string): Promise<ContentScan> {
  if (!existsSync(contentDir)) throw new Error(`Content directory not found: ${contentDir}`);
  const pages: ContentPage[] = [];
  const tree = await walk(contentDir, "", pages);
  const byRoute = new Map<string, ContentPage>();
  for (const page of pages) {
    const existing = byRoute.get(page.route);
    if (existing) {
      throw new Error(`Duplicate route "${page.route}": ${existing.relPath} and ${page.relPath} both map to it.`);
    }
    byRoute.set(page.route, page);
  }
  pages.sort((a, b) => a.route.localeCompare(b.route));
  return { pages, nav: buildNav(tree, true) };
}
