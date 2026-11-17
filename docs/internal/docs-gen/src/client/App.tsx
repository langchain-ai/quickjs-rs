import { useCallback, useEffect, useLayoutEffect, useRef, useState, type MouseEvent } from "react";
import { MDXProvider } from "@mdx-js/react";
import { ListIcon } from "@phosphor-icons/react/dist/ssr/List";
import { XIcon } from "@phosphor-icons/react/dist/ssr/X";
import { FileDashedIcon } from "@phosphor-icons/react/dist/ssr/FileDashed";
import { EmptyState } from "@langchain/macaw-components/EmptyState";
import { IconButton } from "@langchain/macaw-components/IconButton";
import { Link } from "@langchain/macaw-components/Link";
import { Text } from "@langchain/macaw-components/Text";
import { TooltipProvider } from "@langchain/macaw-components/Tooltip";
import { AppThemeProvider } from "@langchain/macaw-components/hooks/AppThemeProvider";
import zIndices from "@langchain/macaw-components/utils/zIndices";
import { nav, site } from "virtual:macaw-docs/site";
import { mdxComponents } from "../components/mdx.tsx";
import { Outline } from "./Outline.tsx";
import { findPage, loadPage, toHref, toRoute } from "./router.ts";
import { Sidebar, SiteTitle } from "./Sidebar.tsx";
import type { LoadedPage, PageModule } from "./types.ts";

interface AppProps {
  route: string;
  page: LoadedPage | null;
}

function scrollToHash(hash: string): boolean {
  if (!hash || hash === "#") return false;
  let id = hash.slice(1);
  try {
    id = decodeURIComponent(id);
  } catch {
    // Use the raw fragment.
  }
  const target = document.getElementById(id);
  if (!target) return false;
  target.scrollIntoView();
  return true;
}

function NotFound() {
  return (
    <div className="flex min-h-[60vh] items-center justify-center">
      <EmptyState
        icon={FileDashedIcon}
        title="Page not found"
        description="This page doesn't exist or has moved."
        action={<Link href={toHref("/")}>Go to the home page</Link>}
      />
    </div>
  );
}

function Article({ page }: { page: LoadedPage }) {
  const { module, entry } = page;
  const Content = module.default;
  const frontmatter = module.frontmatter ?? {};
  const hasHeader = typeof frontmatter.title === "string" && frontmatter.title.trim() !== "";
  const description = typeof frontmatter.description === "string" ? frontmatter.description : "";
  return (
    <article className="min-w-0 flex-1">
      {hasHeader ? (
        <header className="docs-page-header mb-space-6 flex flex-col gap-space-2 border-b border-muted pb-space-5">
          <Text variant="h1" className="text-primary">
            {entry.title}
          </Text>
          {description ? (
            <Text as="p" variant="body" color="secondary" className="max-w-prose text-base">
              {description}
            </Text>
          ) : null}
        </header>
      ) : description ? (
        <Text as="p" variant="body" color="secondary" className="mb-space-5 max-w-prose text-base">
          {description}
        </Text>
      ) : null}
      <div className="docs-prose">
        <Content />
      </div>
    </article>
  );
}

function App({ route: initialRoute, page: initialPage }: AppProps) {
  const [state, setState] = useState({ route: initialRoute, page: initialPage });
  const [drawerOpen, setDrawerOpen] = useState(false);
  const pendingScroll = useRef<string | null>(null);
  const navigation = useRef(0);
  const { route, page } = state;

  const navigate = useCallback(async (url: URL, push: boolean) => {
    const nextRoute = toRoute(url.pathname);
    const token = ++navigation.current;
    const next = await loadPage(nextRoute);
    if (token !== navigation.current) return;
    if (push) window.history.pushState(null, "", url.href);
    pendingScroll.current = url.hash || "top";
    setState({ route: nextRoute, page: next });
    setDrawerOpen(false);
  }, []);

  useLayoutEffect(() => {
    const target = pendingScroll.current;
    if (target === null) return;
    pendingScroll.current = null;
    if (target === "top" || !scrollToHash(target)) window.scrollTo(0, 0);
  }, [state]);

  useEffect(() => {
    const title = page?.entry.title ?? "Page not found";
    document.title = title === site.title ? site.title : `${title} · ${site.title}`;
  }, [page]);

  useEffect(() => {
    const onPopState = () => {
      const url = new URL(window.location.href);
      if (toRoute(url.pathname) === route) {
        if (!scrollToHash(url.hash)) window.scrollTo(0, 0);
        return;
      }
      void navigate(url, false);
    };
    window.addEventListener("popstate", onPopState);
    return () => window.removeEventListener("popstate", onPopState);
  }, [navigate, route]);

  useEffect(() => {
    if (!import.meta.hot) return;
    const onUpdate = (event: Event) => {
      const { path, module } = (event as CustomEvent<{ path: string; module: PageModule }>).detail;
      setState((current) =>
        current.route === path && current.page ? { ...current, page: { ...current.page, module } } : current,
      );
    };
    window.addEventListener("macaw-docs:page-update", onUpdate);
    return () => window.removeEventListener("macaw-docs:page-update", onUpdate);
  }, []);

  useEffect(() => {
    if (!drawerOpen) return;
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") setDrawerOpen(false);
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [drawerOpen]);

  const onClick = (event: MouseEvent<HTMLDivElement>) => {
    if (event.defaultPrevented || event.button !== 0) return;
    if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    const anchor = (event.target as Element).closest("a");
    if (!anchor || !anchor.href) return;
    if ((anchor.target && anchor.target !== "_self") || anchor.hasAttribute("download")) return;
    const url = new URL(anchor.href, window.location.href);
    if (url.origin !== window.location.origin) return;
    const nextRoute = toRoute(url.pathname);
    if (nextRoute === route && url.pathname === window.location.pathname && url.hash) {
      event.preventDefault();
      window.history.pushState(null, "", url.href);
      scrollToHash(url.hash);
      setDrawerOpen(false);
      return;
    }
    if (!findPage(nextRoute)) return;
    event.preventDefault();
    void navigate(url, true);
  };

  const showOutline = page !== null && page.module.frontmatter?.outline !== false && page.module.headings.length > 0;

  return (
    // The delegated click handler intercepts in-site links; anchors stay keyboard accessible.
    // oxlint-disable-next-line jsx-a11y/click-events-have-key-events, jsx-a11y/no-static-element-interactions
    <div className="flex min-h-dvh w-full flex-col bg-surface-level-1 text-primary lg:flex-row" onClick={onClick}>
      <div
        className="sticky top-0 flex h-12 items-center gap-space-2 border-b border-muted bg-sidebar px-space-3 lg:hidden"
        style={{ zIndex: zIndices.sidebar }}
      >
        <IconButton
          icon={drawerOpen ? XIcon : ListIcon}
          iconWeight="regular"
          label={drawerOpen ? "Close navigation" : "Open navigation"}
          aria-expanded={drawerOpen}
          aria-controls="docs-sidebar"
          variant="plain"
          color="secondary"
          size="sm"
          onClick={() => setDrawerOpen((open) => !open)}
        />
        <SiteTitle site={site} />
      </div>
      {drawerOpen ? (
        <div
          aria-hidden
          className="fixed inset-x-0 bottom-0 top-12 bg-overlay lg:hidden"
          style={{ zIndex: zIndices.sidebar }}
          onClick={() => setDrawerOpen(false)}
        />
      ) : null}
      <Sidebar site={site} nav={nav} route={route} open={drawerOpen} zIndex={zIndices.sidebar} />
      <main className="flex min-w-0 flex-1 justify-center">
        <div className="flex w-full max-w-[960px] gap-space-7 px-space-5 pb-space-9 pt-space-6 lg:px-space-7 lg:pt-space-8">
          {page ? <Article key={route} page={page} /> : <NotFound />}
          {showOutline ? (
            <aside className="hidden w-52 shrink-0 xl:block">
              <div className="sticky top-space-8">
                <Outline key={route} headings={page.module.headings} />
              </div>
            </aside>
          ) : null}
        </div>
      </main>
    </div>
  );
}

/** The full app: theme, MDX components, and the docs shell. */
export function Root(props: AppProps) {
  return (
    <AppThemeProvider defaultMode={site.theme}>
      <TooltipProvider>
        <MDXProvider components={mdxComponents}>
          <App {...props} />
        </MDXProvider>
      </TooltipProvider>
    </AppThemeProvider>
  );
}
