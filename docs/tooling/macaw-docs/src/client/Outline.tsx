import { useEffect, useState } from "react";
import { Text } from "@langchain/macaw-components/Text";
import { cn } from "@langchain/macaw-components/utils/cn";
import type { Heading } from "./types.ts";

/** "On this page" list. Re-key it on route change to reset the observer. */
export function Outline({ headings }: { headings: Heading[] }) {
  const [active, setActive] = useState<string | undefined>(headings[0]?.id);

  useEffect(() => {
    const elements = headings
      .map((heading) => document.getElementById(heading.id))
      .filter((el): el is HTMLElement => el !== null);
    if (elements.length === 0) return;
    const visible = new Set<string>();
    const observer = new IntersectionObserver(
      (entries) => {
        for (const entry of entries) {
          if (entry.isIntersecting) visible.add(entry.target.id);
          else visible.delete(entry.target.id);
        }
        const first = headings.find((heading) => visible.has(heading.id));
        if (first) setActive(first.id);
      },
      { rootMargin: "-24px 0px -70% 0px" },
    );
    for (const el of elements) observer.observe(el);
    return () => observer.disconnect();
  }, [headings]);

  if (headings.length === 0) return null;
  return (
    <nav aria-label="On this page" className="flex flex-col gap-space-3">
      <Text variant="h4" as="h2" color="tertiary">
        On this page
      </Text>
      <ul className="flex flex-col gap-space-2 border-l border-muted">
        {headings.map((heading) => (
          <li key={heading.id}>
            <a
              href={`#${heading.id}`}
              aria-current={active === heading.id ? "location" : undefined}
              className={cn(
                "-ml-px block border-l border-transparent text-secondary transition-colors duration-fast hover:text-primary",
                heading.depth === 3 ? "pl-space-5" : "pl-space-3",
                active === heading.id && "border-default text-primary",
              )}
            >
              <Text as="span" variant="sm">
                {heading.text}
              </Text>
            </a>
          </li>
        ))}
      </ul>
    </nav>
  );
}
