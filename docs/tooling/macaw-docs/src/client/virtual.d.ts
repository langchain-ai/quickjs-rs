// Ambient declarations for the plugin's virtual modules. Relative `import type`
// statements are not allowed inside `declare module`, so these use inline
// `import()` types; `skipLibCheck` would otherwise hide the error and turn
// the exports into `any`.

declare module "virtual:macaw-docs/site" {
  export const site: import("../config.ts").SiteConfig;
  export const nav: import("./types.ts").NavItem[];
  export const pages: import("./types.ts").PageEntry[];
}

declare module "virtual:macaw-docs/styles.css" {}
