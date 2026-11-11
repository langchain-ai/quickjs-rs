import {
  Children,
  isValidElement,
  useState,
  type ComponentPropsWithoutRef,
  type ReactElement,
  type ReactNode,
} from "react";
import type { MDXComponents } from "mdx/types";
import { site } from "virtual:macaw-docs/site";
import { ArrowRightIcon } from "@phosphor-icons/react/dist/ssr/ArrowRight";
import { Badge } from "@langchain/macaw-components/Badge";
import { Banner, type BannerIntent } from "@langchain/macaw-components/Banner";
import { Card as MacawCard } from "@langchain/macaw-components/Card";
import { CodeLite, type CodeLiteCodeProps } from "@langchain/macaw-components/Code/CodeLite";
import { resolveCodeLanguage } from "@langchain/macaw-components/Code/codeLanguages";
import { GroupedTabs } from "@langchain/macaw-components/GroupedTabs";
import { Icon as MacawIcon, type IconComponent } from "@langchain/macaw-components/Icon";
import { Kbd, KbdGroup } from "@langchain/macaw-components/Kbd";
import { Link } from "@langchain/macaw-components/Link";
import { Text } from "@langchain/macaw-components/Text";
import { cn } from "@langchain/macaw-components/utils/cn";

/* ------------------------------------------------------------------------ */
/* Callout                                                                  */
/* ------------------------------------------------------------------------ */

const CALLOUT_INTENTS = {
  note: "info",
  tip: "success",
  warning: "warning",
  danger: "error",
} as const satisfies Record<string, BannerIntent>;

interface CalloutProps {
  type?: keyof typeof CALLOUT_INTENTS;
  title?: string;
  children?: ReactNode;
}

function Callout({ type = "note", title, children }: CalloutProps) {
  return (
    <div className="docs-block docs-callout">
      <Banner intent={CALLOUT_INTENTS[type] ?? "info"} title={title}>
        {children}
      </Banner>
    </div>
  );
}

/* ------------------------------------------------------------------------ */
/* Steps                                                                    */
/* ------------------------------------------------------------------------ */

function Steps({ children }: { children?: ReactNode }) {
  return <ol className="docs-block docs-steps flex flex-col gap-space-6">{children}</ol>;
}

function Step({ title, children }: { title?: string; children?: ReactNode }) {
  return (
    <li className="docs-step flex flex-col gap-space-3">
      {title ? (
        <Text variant="h4" className="docs-step-title text-primary">
          {title}
        </Text>
      ) : null}
      <div className="docs-prose">{children}</div>
    </li>
  );
}

/* ------------------------------------------------------------------------ */
/* Tabs                                                                     */
/* ------------------------------------------------------------------------ */

interface TabProps {
  title: string;
  children?: ReactNode;
}

function Tab({ children }: TabProps) {
  return <div className="docs-prose">{children}</div>;
}

function isTab(node: ReactNode): node is ReactElement<TabProps> {
  return isValidElement<TabProps>(node) && typeof node.props.title === "string";
}

function Tabs({ defaultValue, children }: { defaultValue?: string; children?: ReactNode }) {
  const tabs = Children.toArray(children).filter(isTab);
  const titles = tabs.map((tab) => tab.props.title);
  const [value, setValue] = useState(() =>
    defaultValue && titles.includes(defaultValue) ? defaultValue : (titles[0] ?? ""),
  );
  if (tabs.length === 0) return null;
  return (
    <div className="docs-block docs-tabs flex flex-col gap-space-3">
      <GroupedTabs
        size="sm"
        className="self-start"
        value={value}
        onChange={setValue}
        options={titles.map((title) => ({ value: title, display: title }))}
      />
      {tabs.map((tab) => (
        <div key={tab.props.title} hidden={tab.props.title !== value}>
          {tab}
        </div>
      ))}
    </div>
  );
}

/* ------------------------------------------------------------------------ */
/* Cards                                                                    */
/* ------------------------------------------------------------------------ */

function Cards({ cols = 2, children }: { cols?: 2 | 3; children?: ReactNode }) {
  return (
    <div className={cn("docs-block grid grid-cols-1 gap-space-4 sm:grid-cols-2", cols === 3 && "lg:grid-cols-3")}>
      {children}
    </div>
  );
}

function isComponent(value: unknown): value is IconComponent {
  return (
    typeof value === "function" ||
    (typeof value === "object" && value !== null && "$$typeof" in value && !isValidElement(value))
  );
}

/** Prefixes root-relative links written in pages with the site's `base`. */
function withBase(href: string): string {
  return href.startsWith("/") && !href.startsWith("//") && site.base !== "/" ? `${site.base}${href.slice(1)}` : href;
}

interface CardProps {
  title: string;
  href?: string;
  icon?: IconComponent | ReactNode;
  children?: ReactNode;
}

function Card({ title, href, icon, children }: CardProps) {
  const iconNode = isComponent(icon) ? <MacawIcon icon={icon} size="sm" weight="regular" /> : icon;
  const body = (
    <MacawCard
      intent="neutral"
      className={cn(
        "flex h-full flex-col gap-space-2",
        href && "transition-colors duration-normal group-hover:bg-surface-level-2-hover",
      )}
    >
      <div className="flex items-center gap-space-2">
        {iconNode ? <span className="flex shrink-0 text-icon-secondary">{iconNode}</span> : null}
        <Text as="span" variant="md" weight="medium" className="flex-1 text-primary">
          {title}
        </Text>
        {href ? (
          <ArrowRightIcon
            aria-hidden
            size={16}
            weight="regular"
            className="shrink-0 text-icon-tertiary transition-transform duration-normal group-hover:translate-x-0.5 motion-reduce:transition-none"
          />
        ) : null}
      </div>
      {children ? <div className="docs-card-body text-secondary">{children}</div> : null}
    </MacawCard>
  );
  if (!href) return body;
  return (
    <a href={withBase(href)} className="docs-card group block rounded-lg focus-visible:outline-2 focus-visible:outline-offset-2">
      {body}
    </a>
  );
}

/* ------------------------------------------------------------------------ */
/* Code                                                                     */
/* ------------------------------------------------------------------------ */

type CodeLiteLanguage = CodeLiteCodeProps["language"];

const HIGHLIGHTED = new Set<string>(["typescript", "python", "shell", "yaml", "json"] satisfies CodeLiteLanguage[]);

/** Maps a fence language to one CodeLite can highlight, or `plaintext`. */
function toCodeLiteLanguage(lang: string | undefined): CodeLiteLanguage {
  const resolved = resolveCodeLanguage(lang ?? "");
  if (resolved === "javascript") return "typescript";
  return resolved && HIGHLIGHTED.has(resolved) ? (resolved as CodeLiteLanguage) : "plaintext";
}

function textContent(node: ReactNode): string {
  if (node === null || node === undefined || typeof node === "boolean") return "";
  if (typeof node === "string" || typeof node === "number") return String(node);
  if (Array.isArray(node)) return node.map(textContent).join("");
  if (isValidElement<{ children?: ReactNode }>(node)) return textContent(node.props.children);
  return "";
}

interface CodeElementProps {
  "data-language"?: string;
  "data-title"?: string;
  className?: string;
  children?: ReactNode;
}

function Pre({ children }: ComponentPropsWithoutRef<"pre">) {
  const child = Children.toArray(children).find((node) => isValidElement(node)) as
    ReactElement<CodeElementProps> | undefined;
  const props = child?.props ?? {};
  const lang = props["data-language"] ?? /language-(\S+)/.exec(props.className ?? "")?.[1];
  const title = props["data-title"];
  const value = textContent(child ? props.children : children).replace(/\n$/, "");
  return (
    <div className="docs-block docs-code">
      <CodeLite
        variant="block"
        value={value}
        language={toCodeLiteLanguage(lang)}
        label={title ?? lang}
        showGutter={false}
      />
    </div>
  );
}

/* ------------------------------------------------------------------------ */
/* Preview                                                                  */
/* ------------------------------------------------------------------------ */

function Preview({ title, code, children }: { title?: string; code?: string; children?: ReactNode }) {
  return (
    <MacawCard intent="plain" className="docs-block docs-preview flex flex-col overflow-hidden p-0">
      {title ? (
        <div className="border-b border-subtle px-space-4 py-space-2">
          <Text as="span" variant="sm" color="secondary">
            {title}
          </Text>
        </div>
      ) : null}
      <div className="flex flex-wrap items-center gap-space-4 p-space-5">{children}</div>
      {code ? (
        <div className="border-t border-subtle bg-surface-level-2 px-space-4 py-space-3">
          <CodeLite value={code.replace(/^\n+|\n+$/g, "")} language="typescript" showGutter={false} />
        </div>
      ) : null}
    </MacawCard>
  );
}

/* ------------------------------------------------------------------------ */
/* Element overrides                                                        */
/* ------------------------------------------------------------------------ */

function heading(level: 2 | 3) {
  const tag = level === 2 ? "h2" : "h3";
  function DocsHeading({ id, children }: ComponentPropsWithoutRef<"h2">) {
    return (
      <Text
        as={tag}
        variant={tag}
        id={id}
        className={cn("docs-heading group scroll-mt-16 text-primary lg:scroll-mt-6", `docs-${tag}`)}
      >
        {children}
        {id ? (
          <a
            href={`#${id}`}
            aria-label={`Link to ${textContent(children)}`}
            className="docs-anchor ml-space-2 text-tertiary no-underline opacity-0 transition-opacity duration-fast hover:text-link focus-visible:opacity-100 group-hover:opacity-100"
          >
            #
          </a>
        ) : null}
      </Text>
    );
  }
  return DocsHeading;
}

const EXTERNAL_HREF = /^(?:[a-z][a-z\d+.-]*:|\/\/)/i;

function Anchor({ href, children, ...rest }: ComponentPropsWithoutRef<"a">) {
  if (!href) return <span>{children}</span>;
  const external = EXTERNAL_HREF.test(href);
  return (
    <Link
      {...rest}
      href={withBase(href)}
      variant="body"
      className="docs-link"
      {...(external ? { target: "_blank", rel: "noreferrer" } : {})}
    >
      {children}
    </Link>
  );
}

function InlineCode({ className, ...props }: ComponentPropsWithoutRef<"code">) {
  return (
    <code
      {...props}
      className={cn(
        "docs-inline-code rounded-xs bg-surface-level-3 px-space-1 py-px font-mono text-[0.9em] tracking-normal text-secondary",
        className,
      )}
    />
  );
}

function Table(props: ComponentPropsWithoutRef<"table">) {
  return (
    // A focusable scroll region, so keyboard users can scroll wide tables (as in MarkdownDisplay).
    <section
      className="docs-block docs-table w-full overflow-x-auto rounded-md border border-default"
      aria-label="Table"
      // oxlint-disable-next-line jsx-a11y/no-noninteractive-tabindex
      tabIndex={0}
    >
      <table {...props} className="w-full min-w-max border-collapse text-left text-sm" />
    </section>
  );
}

/** Components available in every page without imports. */
export const mdxComponents: MDXComponents = {
  Callout,
  Steps,
  Step,
  Tabs,
  Tab,
  Cards,
  Card,
  Preview,
  Badge,
  Kbd,
  KbdGroup,
  Icon: MacawIcon,
  pre: Pre,
  code: InlineCode,
  h2: heading(2),
  h3: heading(3),
  a: Anchor,
  table: Table,
};
