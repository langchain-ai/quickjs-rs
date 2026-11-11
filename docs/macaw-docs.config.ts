import { defineConfig } from "macaw-docs";

export default defineConfig({
  title: "quickjs-rs",
  tagline: "docs",
  description: "Sandboxed JavaScript for Python: QuickJS compiled to WebAssembly, driven by wasmtime.",
  logo: "langchain",
  content: "content",
  base: process.env.DOCS_BASE ?? "/quickjs-rs/",
  links: [
    { label: "GitHub", href: "https://github.com/langchain-ai/quickjs-rs" },
    { label: "PyPI", href: "https://pypi.org/project/quickjs-rs/" },
  ],
});
