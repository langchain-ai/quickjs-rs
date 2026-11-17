import { useState } from "react";
import { CaretDownIcon } from "@phosphor-icons/react/dist/ssr/CaretDown";
import { CaretRightIcon } from "@phosphor-icons/react/dist/ssr/CaretRight";
import { IconButton } from "@langchain/macaw-components/IconButton";
import { Link } from "@langchain/macaw-components/Link";
import { Logo } from "@langchain/macaw-components/Logo";
import { Text } from "@langchain/macaw-components/Text";
import { cn } from "@langchain/macaw-components/utils/cn";
import type { SiteConfig } from "../config.ts";
import { toHref } from "./router.ts";
import { ThemeToggle } from "./ThemeToggle.tsx";
import type { NavItem } from "./types.ts";

function containsRoute(item: NavItem, route: string): boolean {
  if (item.path === route) return true;
  return item.type === "group" && item.items.some((child) => containsRoute(child, route));
}

const itemClass =
  "flex min-h-8 items-center rounded-md px-space-2 py-space-1 text-secondary transition-colors duration-fast hover:bg-sidebar-hover hover:text-primary";

function NavLink({ title, path, route, strong }: { title: string; path: string; route: string; strong?: boolean }) {
  const active = path === route;
  return (
    <a
      href={toHref(path)}
      aria-current={active ? "page" : undefined}
      className={cn(itemClass, "min-w-0 flex-1", active && "bg-selected text-primary hover:bg-selected")}
    >
      <Text as="span" variant="sm" weight={strong || active ? "medium" : undefined} className="truncate">
        {title}
      </Text>
    </a>
  );
}

function NavGroup({ item, route }: { item: Extract<NavItem, { type: "group" }>; route: string }) {
  const containsCurrent = containsRoute(item, route);
  const [open, setOpen] = useState(!item.collapsed || containsCurrent);
  // Re-open when navigation moves the current page into this group.
  const [wasCurrent, setWasCurrent] = useState(containsCurrent);
  if (containsCurrent !== wasCurrent) {
    setWasCurrent(containsCurrent);
    if (containsCurrent) setOpen(true);
  }
  return (
    <li className="flex flex-col gap-px">
      <div className="flex items-center gap-space-1">
        {item.path ? (
          <NavLink title={item.title} path={item.path} route={route} strong />
        ) : (
          <div className={cn(itemClass, "min-w-0 flex-1 hover:bg-transparent hover:text-secondary")}>
            <Text as="span" variant="sm" weight="medium" className="truncate text-primary">
              {item.title}
            </Text>
          </div>
        )}
        <IconButton
          icon={open ? CaretDownIcon : CaretRightIcon}
          iconWeight="regular"
          label={`${open ? "Collapse" : "Expand"} ${item.title}`}
          aria-expanded={open}
          variant="plain"
          color="secondary"
          size="xs"
          onClick={() => setOpen((value) => !value)}
        />
      </div>
      {open && item.items.length > 0 ? (
        <div className="ml-space-3 border-l border-muted pl-space-2">
          <NavList items={item.items} route={route} />
        </div>
      ) : null}
    </li>
  );
}

function NavList({ items, route }: { items: NavItem[]; route: string }) {
  return (
    <ul className="flex flex-col gap-px">
      {items.map((item) =>
        item.type === "page" ? (
          <li key={item.path} className="flex">
            <NavLink title={item.title} path={item.path} route={route} />
          </li>
        ) : (
          <NavGroup key={`${item.title}:${item.path ?? ""}`} item={item} route={route} />
        ),
      )}
    </ul>
  );
}

export function SiteTitle({ site }: { site: SiteConfig }) {
  return (
    <a href={toHref("/")} className="flex min-w-0 items-center gap-space-2 rounded-md text-primary">
      {site.logo ? <Logo brand={site.logo} variant="logomark" size="sm" /> : null}
      <Text as="span" variant="md" weight="semibold" className="truncate">
        {site.title}
      </Text>
      {site.tagline ? (
        <Text as="span" variant="sm" color="tertiary" className="truncate">
          {site.tagline}
        </Text>
      ) : null}
    </a>
  );
}

export interface SidebarProps {
  site: SiteConfig;
  nav: NavItem[];
  route: string;
  open: boolean;
  zIndex: number;
}

export function Sidebar({ site, nav, route, open, zIndex }: SidebarProps) {
  return (
    <aside
      id="docs-sidebar"
      aria-label="Site"
      style={{ zIndex }}
      className={cn(
        "docs-sidebar fixed bottom-0 left-0 top-12 w-72 max-w-[85vw] flex-col border-r border-muted bg-sidebar",
        "lg:sticky lg:top-0 lg:flex lg:h-dvh lg:max-w-none lg:shrink-0",
        open ? "flex" : "hidden",
      )}
    >
      <div className="hidden px-space-5 pb-space-4 pt-space-5 lg:block">
        <SiteTitle site={site} />
      </div>
      <nav aria-label="Documentation" className="min-h-0 flex-1 overflow-y-auto px-space-3 py-space-3 lg:pt-0">
        <NavList items={nav} route={route} />
      </nav>
      <div className="flex items-center gap-space-3 border-t border-muted px-space-5 py-space-3">
        <ul className="flex min-w-0 flex-1 flex-wrap gap-x-space-3 gap-y-space-1">
          {site.links.map((link) => (
            <li key={link.href}>
              <Link href={link.href} variant="sm">
                {link.label}
              </Link>
            </li>
          ))}
        </ul>
        <ThemeToggle />
      </div>
    </aside>
  );
}
