import type { Expression, Program } from "estree";
import type { Code, Heading as MdastHeading, Nodes, Root } from "mdast";
import type { Heading } from "../client/types.ts";

/** GitHub-style heading slugs, de-duplicated per page (`foo`, `foo-1`). */
function createSlugger(): (text: string) => string {
  const seen = new Map<string, number>();
  return (text) => {
    const base =
      text
        .toLowerCase()
        .trim()
        .replace(/[^\p{L}\p{N}\s_-]/gu, "")
        .replace(/\s/g, "-") || "section";
    let slug = base;
    const previous = seen.get(base);
    if (previous !== undefined) {
      let count = previous;
      do {
        count += 1;
        slug = `${base}-${count}`;
      } while (seen.has(slug));
      seen.set(base, count);
    }
    seen.set(slug, 0);
    return slug;
  };
}

function textOf(node: Nodes): string {
  if ("value" in node && typeof node.value === "string") {
    return node.type === "text" || node.type === "inlineCode" ? node.value : "";
  }
  if ("children" in node) return (node.children as Nodes[]).map(textOf).join("");
  return "";
}

function visit(node: Nodes, fn: (node: Nodes) => void): void {
  fn(node);
  if ("children" in node) for (const child of node.children as Nodes[]) visit(child, fn);
}

/** Parses `title="…"` or `title='…'` from a code fence's meta string. */
function parseCodeTitle(meta: string | null | undefined): string | undefined {
  if (!meta) return undefined;
  const match = /(?:^|\s)title=(?:"([^"]*)"|'([^']*)')/.exec(meta);
  return match ? (match[1] ?? match[2]) : undefined;
}

/** Minimal JSON → estree converter for the exported page data. */
function toEstree(value: unknown): Expression {
  if (Array.isArray(value)) return { type: "ArrayExpression", elements: value.map(toEstree) };
  if (value && typeof value === "object") {
    return {
      type: "ObjectExpression",
      properties: Object.entries(value)
        .filter(([, v]) => v !== undefined)
        .map(([key, v]) => ({
          type: "Property",
          key: { type: "Literal", value: key },
          value: toEstree(v),
          kind: "init",
          method: false,
          shorthand: false,
          computed: false,
        })),
    };
  }
  if (typeof value === "string" || typeof value === "boolean" || value === null) {
    return { type: "Literal", value };
  }
  if (typeof value === "number" && Number.isFinite(value)) {
    return value < 0
      ? { type: "UnaryExpression", operator: "-", prefix: true, argument: { type: "Literal", value: -value } }
      : { type: "Literal", value };
  }
  if (value instanceof Date) return { type: "Literal", value: value.toISOString() };
  return { type: "Literal", value: null };
}

function exportConst(name: string, value: unknown): Root["children"][number] {
  const program: Program = {
    type: "Program",
    sourceType: "module",
    body: [
      {
        type: "ExportNamedDeclaration",
        specifiers: [],
        source: null,
        attributes: [],
        declaration: {
          type: "VariableDeclaration",
          kind: "const",
          declarations: [{ type: "VariableDeclarator", id: { type: "Identifier", name }, init: toEstree(value) }],
        },
      },
    ],
  };
  return { type: "mdxjsEsm", value: "", data: { estree: program } } as Root["children"][number];
}

export interface RemarkDocsOptions {
  /** Frontmatter parsed during `transform`, keyed by absolute file path. */
  frontmatter: Map<string, Record<string, unknown>>;
}

/**
 * Adds heading ids and code-fence data attributes, then exports `headings`
 * and `frontmatter` from every page.
 */
export function remarkDocs(options: RemarkDocsOptions) {
  return (tree: Root, file: { path?: string }) => {
    const slug = createSlugger();
    const headings: Heading[] = [];
    visit(tree, (node) => {
      if (node.type === "heading") {
        const heading = node as MdastHeading;
        if (heading.depth !== 2 && heading.depth !== 3) return;
        const text = textOf(heading).trim();
        const id = slug(text);
        heading.data = { ...heading.data, hProperties: { ...heading.data?.hProperties, id } };
        headings.push({ depth: heading.depth, id, text });
      } else if (node.type === "code") {
        const code = node as Code;
        const title = parseCodeTitle(code.meta);
        // mdast-util-to-hast applies these to the inner <code>, not <pre>.
        code.data = {
          ...code.data,
          hProperties: {
            ...code.data?.hProperties,
            ...(code.lang ? { "data-language": code.lang } : {}),
            ...(title ? { "data-title": title } : {}),
          },
        };
      }
    });
    const key = file.path?.replace(/[?#].*$/, "");
    const frontmatter = (key && options.frontmatter.get(key)) || {};
    tree.children.push(exportConst("headings", headings), exportConst("frontmatter", frontmatter));
  };
}
