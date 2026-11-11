import type { ComponentType } from "react";
import type { MDXComponents } from "mdx/types";

/** A `##` or `###` heading collected for the "On this page" outline. */
export interface Heading {
  depth: 2 | 3;
  id: string;
  text: string;
}

interface SidebarFrontmatter {
  label?: string;
  order?: number;
  hidden?: boolean;
}

export interface Frontmatter {
  title?: string;
  description?: string;
  sidebar?: SidebarFrontmatter;
  outline?: boolean;
  [key: string]: unknown;
}

export type NavItem =
  | { type: "page"; title: string; path: string }
  | {
      type: "group";
      title: string;
      path?: string;
      collapsed: boolean;
      items: NavItem[];
    };

/** The module a compiled `.md`/`.mdx` page exports. */
export interface PageModule {
  default: ComponentType<{ components?: MDXComponents }>;
  headings: Heading[];
  frontmatter: Frontmatter;
}

/** One entry of `pages` in `virtual:macaw-docs/site`. */
export interface PageEntry {
  path: string;
  title: string;
  description: string;
  load: () => Promise<PageModule>;
}

export interface LoadedPage {
  entry: PageEntry;
  module: PageModule;
}
