# Writing the quickjs-rs docs

The site lives in `content/` and is built by the vendored Macaw builder in `internal/docs-gen`. These rules apply to
every page, whether a person or an agent writes it.

## Shape of a page

- Open with one or two plain sentences on the problem the feature solves. No summary boxes.
- Show a runnable example right after. Then explain it. Put caveats after the examples, not before.
- Use a table for settings, defaults, and conversions. Use prose for behavior.
- Keep pages short. Link to [Architecture](content/reference/architecture.mdx) for internals instead of explaining
  them on concept pages.

## Voice

Follows the [LangChain docs voice](https://github.com/langchain-ai/docs/blob/main/.agents/skills/docs-team-voice/SKILL.md):

- Short sentences, one idea each. Split anything over about 25 words.
- Second person, active voice, present tense. No "we", no "let's".
- No contractions, no em dashes, no filler ("simply", "just", "basically").
- State defaults and conditions as plain facts: "The default is 64 MiB." Backtick every identifier and value.
- Never invent a value, field, or behavior. If you cannot verify it, leave it out.

## Verify every claim

1. **Examples run.** Every ```` ```python ```` block is a standalone program. `tests/test_docs.py` runs each one and
   compares what it prints with its `#>` comment lines, in order. Mark a block that cannot run (a signature, a
   fragment) with `test="skip"` in the fence.
2. **Prose claims are checked against the code**, not against other docs. Run a scratch script against the local
   build (`python scripts/build_guest.py && pip install -e .`), or read the source in `quickjs_rs/` and `crates/`.
   Name anything you could not verify in the PR description.
3. **Voice rules a regex can catch** (contractions, em dashes, filler, first person) are enforced by
   `tests/test_docs.py::test_prose`.
4. **Links resolve.** `pnpm check` builds the site and fails on any broken internal link or anchor.

```bash
pytest tests/test_docs.py      # examples and prose rules
cd docs && pnpm check          # build and link check
```
