#!/usr/bin/env node
import { parseArgs } from "node:util";

const MIN_NODE_MAJOR = 26;
const major = Number(process.versions.node.split(".")[0]);
if (major < MIN_NODE_MAJOR) {
  console.error(`macaw-docs requires Node ${MIN_NODE_MAJOR} or newer (found ${process.versions.node}).`);
  process.exit(1);
}

const USAGE = `Usage: macaw-docs <dev|build|preview> [root] [--port <n>] [--host]

Commands:
  dev       Start the dev server
  build     Pre-render the site to static HTML
  preview   Serve the built site locally

Options:
  --port <n>  Port for dev or preview
  --host      Listen on all addresses
  -h, --help  Show this message`;

async function main(): Promise<void> {
  const { positionals, values } = parseArgs({
    allowPositionals: true,
    options: {
      port: { type: "string" },
      host: { type: "boolean" },
      help: { type: "boolean", short: "h" },
    },
  });

  const [command, root = "."] = positionals;
  if (values.help || !command) {
    console.log(USAGE);
    process.exit(command || values.help ? 0 : 1);
  }

  const port = values.port === undefined ? undefined : Number(values.port);
  if (port !== undefined && (!Number.isInteger(port) || port <= 0)) {
    throw new Error(`--port must be a positive integer (got "${values.port}").`);
  }

  // Set before anything loads React, which picks its build from NODE_ENV once.
  process.env.NODE_ENV ??= command === "dev" ? "development" : "production";
  const { build, dev, preview } = await import("../src/node/build.ts");
  switch (command) {
    case "dev":
      await dev(root, { port, host: values.host });
      break;
    case "build":
      await build(root);
      break;
    case "preview":
      await preview(root, { port, host: values.host });
      break;
    default:
      throw new Error(`Unknown command "${command}".\n\n${USAGE}`);
  }
}

main().catch((error: unknown) => {
  if (error instanceof Error) console.error(process.env.MACAW_DOCS_DEBUG ? error.stack : error.message);
  else console.error(error);
  process.exit(1);
});
