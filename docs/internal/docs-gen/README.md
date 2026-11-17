# macaw-docs

> Vendored copy of the experimental `macaw-docs` builder, trimmed to what this site needs (no example
> site, tests or lint config). Local change: root-relative links in pages are prefixed with `base`.

Static MDX documentation sites styled with [Macaw](https://macaw.langchain.dev), LangChain's design system.
Put `.md`/`.mdx` files in `docs/`, run the CLI, and ship pre-rendered HTML that hydrates into a client app
which moves between pages without full reloads.

## Requirements

- Node **26 or newer** (Macaw's engine floor). The CLI runs TypeScript directly through Node's type stripping.
- React 19 and Tailwind 4 come with the package.

## Usage

```sh
macaw-docs dev [root] [--port 5173] [--host]   # dev server with hot updates
macaw-docs build [root]                        # pre-render to dist/
macaw-docs preview [root] [--port n] [--host]  # serve dist/ like a static host
```

`root` holds a `macaw-docs.config.ts` (or `.mjs` / `.js`):

```ts
import { defineConfig } from "macaw-docs";

export default defineConfig({
  title: "Acme Docs",
  links: [{ label: "GitHub", href: "https://github.com/acme/acme" }],
});
```

| Option        | Default    | Notes                                                                    |
| ------------- | ---------- | ------------------------------------------------------------------------ |
| `title`       | required   | Site name in the sidebar and page titles.                                |
| `tagline`     | `""`       | Short label after the site title.                                        |
| `description` | `""`       | Default meta description.                                                |
| `logo`        | none       | Macaw `Logo` brand (`langchain`, `langsmith`, `fleet`, `engine`).        |
| `content`     | `"docs"`   | Relative to the config file.                                             |
| `outDir`      | `"dist"`   | Relative to the config file.                                             |
| `base`        | `"/"`      | Normalized to a leading and trailing slash.                              |
| `links`       | `[]`       | `{ label, href }`, rendered in the sidebar footer.                       |
| `theme`       | `"system"` | `"light"`, `"dark"`, or `"system"`; the initial mode before user choice. |
| `vite`        | `{}`       | Merged into dev and build with `mergeConfig`.                            |

Routes, frontmatter, folder `meta.*` files, nav ordering, and the built-in components (`Callout`, `Steps`,
`Tabs`, `Cards`, `Preview`, `Badge`, `Kbd`, `Icon`) are covered in `src/components/mdx.tsx` and `src/node/content.ts`.

## Design Notes

- **Macaw is a dependency, not vendored.** Components are imported through direct subpaths
  (`@langchain/macaw-components/Banner`, `/Code/CodeLite`, …). `resolveCodeLanguage` comes from
  `/Code/codeLanguages` rather than the `/Code` index, which would statically pull in the CodeMirror
  editor and the rich-text editor.
- **Stylesheet layers.** `src/client/styles.css` imports Macaw's `styles.css` (fonts, tokens, base, and
  precompiled component utilities), then only Tailwind's `theme` and `utilities` layers. Macaw's
  precompiled CSS already contains Tailwind's preflight, so importing all of `tailwindcss` emitted it a
  second time. Tailwind's automatic source detection is off (`source(none)`); the generated virtual
  stylesheet registers this package's `src/` and the site's content folder with `@source`.
- **Prose** (`.docs-prose`) uses `:where()` selectors inside `@layer components`, and only targets
  class-less (authored Markdown) elements, so Macaw components always win.
- **SSR externals.** The server bundle inlines `@langchain/macaw-components` (ESM that imports CSS) and
  leaves other `node_modules` imports external but rewritten to absolute paths, so prerendering works
  under pnpm's strict layout no matter where `outDir` is.
- **React builds.** The CLI sets `NODE_ENV` before anything loads React, and the build module only loads
  Macaw's theme constants lazily, so the server render never mixes React's development and production
  builds.

## Upgrading Macaw

The Macaw version is pinned. To upgrade:

1. Bump `@langchain/macaw-components` and `@langchain/macaw-cli` together to the same exact version, and
   `@phosphor-icons/react` to the version Macaw depends on. pnpm 11 rejects packages published less than a day
   ago (`minimumReleaseAge`), so pick a release at least a day old rather than adding exemptions.
2. Re-read `node_modules/@langchain/macaw-components/docs/DESIGN.md` and `STYLES.md`.
3. Run `pnpm macaw inspect <Name> --json` for each mapped component (`Banner`, `GroupedTabs`, `Card`,
   `CodeLite`, `Badge`, `Kbd`, `Icon`, `Text`, `Link`, `IconButton`, `Logo`, `EmptyState`, `Tooltip`) and check
   their props.
4. Rebuild, run the tests, and re-screenshot light, dark, and mobile against the Storybook.

## Out of Scope for v1

- Search.
- A compiled `dist/` for publishing. Node does not strip types for files under `node_modules`, so the
  package currently runs from source (for example, a workspace or `file:` dependency).
- Tailwind classes in files outside the package `src/` and the content folder (such as a site's own
  components folder) are not scanned.
