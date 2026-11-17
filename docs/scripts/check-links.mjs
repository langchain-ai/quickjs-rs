// Check every internal link and anchor in the built site (dist/).
// Run after `pnpm build`: `pnpm check`.
import { readFileSync, readdirSync, statSync } from "node:fs";
import { join, relative } from "node:path";

const dist = new URL("../dist/", import.meta.url).pathname;
const base = (process.env.DOCS_BASE ?? "/quickjs-rs/").replace(/\/?$/, "/");

function pages(dir) {
  return readdirSync(dir).flatMap((name) => {
    const path = join(dir, name);
    if (statSync(path).isDirectory()) return pages(path);
    return name.endsWith(".html") ? [path] : [];
  });
}

const ids = new Map();
const files = pages(dist);
for (const file of files) {
  const route = "/" + relative(dist, file).replace(/index\.html$/, "").replace(/\.html$/, "/");
  const html = readFileSync(file, "utf8");
  ids.set(route.replace(/\/$/, "") || "/", new Set([...html.matchAll(/\sid="([^"]+)"/g)].map((m) => m[1])));
}

const problems = [];
for (const file of files) {
  const html = readFileSync(file, "utf8");
  for (const [, href] of html.matchAll(/\shref="([^"]+)"/g)) {
    if (!href.startsWith(base) || /\.(css|js|svg|png|ico|xml|txt)(\?|$)/.test(href)) continue;
    const [path, hash] = href.slice(base.length - 1).split("#");
    const route = path.replace(/\/$/, "") || "/";
    const anchors = ids.get(route);
    if (!anchors) problems.push(`${relative(dist, file)}: missing page ${href}`);
    else if (hash && !anchors.has(decodeURIComponent(hash))) {
      problems.push(`${relative(dist, file)}: missing anchor ${href}`);
    }
  }
}

if (problems.length) {
  console.error(problems.join("\n"));
  process.exit(1);
}
console.log(`checked ${files.length} pages: all internal links resolve`);
