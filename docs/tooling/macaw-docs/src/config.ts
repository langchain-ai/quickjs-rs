import type { UserConfig } from "vite";

export type ThemeMode = "light" | "dark" | "system";
export type LogoBrand = "langchain" | "langsmith" | "fleet" | "engine";

export interface DocsLink {
  label: string;
  href: string;
}

/** Options accepted by `macaw-docs.config.ts`. */
export interface DocsConfig {
  /** Site name shown in the sidebar and in page titles. */
  title: string;
  /** Short label shown after the site title. */
  tagline?: string;
  /** Default meta description. */
  description?: string;
  /** Optional Macaw `Logo` brand shown before the title. */
  logo?: LogoBrand;
  /** Content directory, relative to the config file. Defaults to `docs`. */
  content?: string;
  /** Output directory, relative to the config file. Defaults to `dist`. */
  outDir?: string;
  /** Public base path. Defaults to `/`. */
  base?: string;
  /** Links rendered in the sidebar footer. */
  links?: DocsLink[];
  /** Initial color scheme. Defaults to `system`. */
  theme?: ThemeMode;
  /** Extra Vite config merged into dev and build. */
  vite?: UserConfig;
}

/** Config after defaults and absolute paths are applied. */
export interface ResolvedDocsConfig {
  root: string;
  configFile: string;
  title: string;
  tagline: string;
  description: string;
  logo: LogoBrand | undefined;
  contentDir: string;
  outDir: string;
  base: string;
  links: DocsLink[];
  theme: ThemeMode;
  vite: UserConfig;
}

/** Browser-safe subset of the config, embedded in the client bundle. */
export interface SiteConfig {
  title: string;
  tagline: string;
  description: string;
  logo: LogoBrand | null;
  base: string;
  links: DocsLink[];
  theme: ThemeMode;
}

export function toSiteConfig(config: ResolvedDocsConfig): SiteConfig {
  return {
    title: config.title,
    tagline: config.tagline,
    description: config.description,
    logo: config.logo ?? null,
    base: config.base,
    links: config.links,
    theme: config.theme,
  };
}

export function normalizeBase(base: string | undefined): string {
  const trimmed = (base ?? "/").trim();
  if (trimmed === "" || trimmed === "/") return "/";
  return `/${trimmed.replace(/^\/+|\/+$/g, "")}/`;
}
