import type { DocsConfig } from "./config.ts";

export type { DocsConfig, DocsLink, LogoBrand, ThemeMode } from "./config.ts";

/** Identity helper that types a `macaw-docs.config.ts` default export. */
export function defineConfig(config: DocsConfig): DocsConfig {
  return config;
}
